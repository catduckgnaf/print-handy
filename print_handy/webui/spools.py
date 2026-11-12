"""Web UI spool management endpoints and page (#789).

``GET /api/printers/<id>/spools`` is the #744 loaded view as JSON: the
printer's slots with their assigned spool (id, name, material, color,
remaining weight and percent where known), plus the unassigned spools
on the shelf, from Spoolman. Slot assignment is read from the Spoolman
``ph_slot`` extra field (#798) — the reconciled state — with the local
slot cache as the fallback for assignments Spoolman has not been
migrated to yet, and duplicate or archived claims are named as
conflicts in the response instead of silently picking a winner.

``POST /api/printers/<id>/spools/{assign,swap,unassign,add,edit,sync}``
makes the changes, always through the operations Discord uses — never
a second state store:

- assign goes through :func:`print_handy.runtime.spool_sync.bind_slot`
  (a replaced spool goes back on the shelf, bind_slot's default);
- swap goes through ``spool_sync.swap_slots`` and unassign through
  ``spool_sync.unbind_slot``, the primitives beside ``bind_slot``;
- add reuses ``spool_sync.create_spool`` (the #512 creation path,
  without the chat walkthrough);
- edit writes the spool's Spoolman record through the Spoolman client:
  remaining weight and archive/unarchive on the spool itself, and
  name, material, color and brand/vendor on its filament record (the
  record Spoolman's own UI edits; every spool of that filament type
  shares it, which the response says out loud);
- sync runs ``spool_sync.reconcile`` for that printer.

Guard posture mirrors the print controls (#708, controls.py): every
POST first checks ``[webui] controls_enabled`` and answers 403 with
the controls off reason while it is off, sending nothing; the server
additionally requires the ``X-WebUI-Token`` header for API POSTs (a
login session never authorizes one), exactly as for control POSTs.
Every operation names its printer in the path; there is no
default-printer fallback anywhere — an unknown id is a 404 on every
route. Refusals are named in the response: unknown spool or slot,
reconcile conflicts, and the SpoolError wording bind_slot itself uses.

Mid-print posture is Discord's, unchanged: the chat flows place no
extra refusal on an assignment change while a print runs — bind_slot
is an inventory write whose screen push defers while printing and
whose usage repair (in reconcile) leaves a busy printer alone — so
these endpoints inherit exactly that behavior by calling the same
functions, and invent no new rule.

The #801 hook: ``purchase/propose`` / ``purchase/create`` back the
Spools page's paste-a-link action. A link is proposed through
``purchase_intake.propose_from_link`` only when an AI route is
configured; with none, the action refuses plainly naming why (and the
page omits the form). Nothing is created by proposing: creation runs
``purchase_intake.create_purchase`` only after the page's explicit
Create action posts the proposed lines back.

Each function returns ``(http_code, body)`` in the Web UI's ``{ok,
reason}`` shape. ``client`` (the Spoolman transport), ``mr`` (the
Moonraker transport) and the state paths are injectable for tests,
which use fakes only.
"""
from __future__ import annotations

import html
import json
import logging
from urllib.parse import quote

from .. import credential_binding, settings
from ..runtime import purchase_intake, spool_sync
from . import controls as controls_module
from . import shell

log = logging.getLogger(__name__)

ACTIONS = ("assign", "swap", "unassign", "add", "edit", "sync")
PURCHASE_ACTIONS = ("purchase/propose", "purchase/create")
SLOTS = spool_sync.SLOTS

NO_AI_REASON = ("purchase link intake needs an AI route, and none is configured: "
                "set PRINT_VISION_URL or services.vision_chat_url to use it. "
                "Nothing was created.")


class SpoolsError(Exception):
    """A refusal with its HTTP code; nothing was changed."""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


# ------------------------------------------------------------------ plumbing

def _client(client):
    return spool_sync.spoolman if client is None else client


def _mr(mr, printer_id):
    return spool_sync.moonraker_for(printer_id) if mr is None else mr


def _resolve_read(data, printer_id):
    try:
        return settings.by_id(printer_id, data)
    except settings.SettingsError:
        raise SpoolsError(404, f"no printer with id {printer_id!r} is configured") from None


def _resolve_write(data, printer_id):
    """The #708 guard, in controls.py's order: the off switch first,
    then the named printer (an unknown id is a 404, never a fallback)."""
    if not controls_module.controls_enabled(data):
        raise SpoolsError(403, controls_module.OFF_REASON)
    return _resolve_read(data, printer_id)


def _call(func, *args, **kwargs):
    """Run one spool operation, mapping its refusals onto HTTP codes.

    spool_sync.SpoolError is a named refusal (unknown spool, archived
    spool, slot conflicts): 409, with its own wording. Any other failure
    from the Spoolman or Moonraker transports is a 502 naming that
    nothing was changed by this call's own bookkeeping layer.
    """
    try:
        return func(*args, **kwargs)
    except spool_sync.SpoolError as refusal:
        raise SpoolsError(409, str(refusal)) from None
    except SpoolsError:
        raise
    except Exception as exc:
        # The exception class/repr is operator noise (#840): it goes to
        # the log, and the wording the operator sees stays plain.
        log.warning("spool operation failed: %s: %s", type(exc).__name__, exc)
        raise SpoolsError(502, "the spool operation did not complete. "
                               "Run the spool sync to converge the slot state.") from None


def _int_field(body, name, *, required=True):
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value == int(value):
            value = int(value)
        elif required:
            raise SpoolsError(400, f"{name} must be a whole number. Nothing changed.")
        else:
            return None
    return int(value)


def _slot_field(body, name="slot"):
    slot = _int_field(body, name)
    if slot not in SLOTS:
        raise SpoolsError(400, f"{name} {slot} is not a slot (slots are "
                               f"{SLOTS[0]}-{SLOTS[-1]}). Nothing changed.")
    return slot


def _body(body):
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise SpoolsError(400, "the request body must be a JSON object. Nothing changed.")
    return body


# ------------------------------------------------------------------ summaries

def spool_summary(row, cache_rec=None):
    """One spool record as the API and the page show it.

    The Spoolman filament record supplies identity (its name is the
    color name, as everywhere in Print Handy); the local slot record
    is the fallback when Spoolman has no row for a cached assignment.
    A percent is given only when the spool's starting weight is
    recorded — never estimated (#744's rule).
    """
    filament = (row or {}).get("filament") or {}
    cache_rec = cache_rec or {}
    remaining = (row or {}).get("remaining_weight")
    if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
        remaining = cache_rec.get("remaining_grams")
    initial = (row or {}).get("initial_weight")
    percent = None
    if isinstance(remaining, (int, float)) and isinstance(initial, (int, float)) \
            and initial > 0:
        percent = round(remaining / initial * 100, 1)
    vendor = filament.get("vendor") or {}
    color = str(filament.get("name") or cache_rec.get("color") or "").strip()
    material = str(filament.get("material") or cache_rec.get("material") or "").strip()
    from ..runtime.discord_print_summary import filament_label
    name = filament_label(color, material) or str(cache_rec.get("name") or "").strip()
    spool_id = (row or {}).get("id", cache_rec.get("spoolman_id"))
    return {
        "id": int(spool_id) if spool_id is not None else None,
        "name": name or f"Spool #{spool_id}",
        "color": color,
        "material": material,
        "brand": str(vendor.get("name") or "").strip() or None,
        "color_hex": spool_sync.screen_hex(filament.get("color_hex"))
                     or spool_sync.screen_hex(cache_rec.get("hex")),
        "remaining_weight": float(remaining) if isinstance(remaining, (int, float)) else None,
        "initial_weight": float(initial) if isinstance(initial, (int, float)) else None,
        "remaining_percent": percent,
        "archived": bool((row or {}).get("archived")),
        "location": (row or {}).get("location"),
    }


def _load_cache(printer_id, state_path):
    try:
        return spool_sync.load_state(state_path, printer_id=printer_id)
    except Exception:  # no readable cache: Spoolman's claims are the whole view
        return {"filament_slots": {}}


def spools_view(data, printer_id, *, client=None, state_path=None):
    """(code, body) for GET /api/printers/<id>/spools."""
    try:
        entry = _resolve_read(data, printer_id)
        client = _client(client)
        try:
            rows = client("GET", "/api/v1/spool?allow_archived=true", None)
        except Exception as exc:
            log.warning("spool view: Spoolman did not answer: %s: %s",
                        type(exc).__name__, exc)
            raise SpoolsError(502, "Spoolman did not answer, "
                                   "so the spool view cannot be built") from None
        if not isinstance(rows, list):
            raise SpoolsError(502, "Spoolman did not answer with a spool list, "
                                   "so the spool view cannot be built")
        spools = [row for row in rows if isinstance(row, dict) and row.get("id") is not None]
        by_id = {int(row["id"]): row for row in spools}
        state = _load_cache(printer_id, state_path)
        cache_slots = state.get("filament_slots") or {}
        claims: dict[int, list[dict]] = {}
        claimed_anywhere: set[int] = set()
        for row in spools:
            parsed = spool_sync.spool_ph_slot(row)
            if parsed is None:
                continue
            claimed_anywhere.add(int(row["id"]))
            if parsed[0] == printer_id:
                claims.setdefault(parsed[1], []).append(row)
        conflicts: list[str] = []
        slot_views = []
        cache_loaded: set[int] = set()
        for slot in SLOTS:
            cache_rec = cache_slots.get(str(slot)) or {}
            if cache_rec.get("spoolman_id") is not None:
                cache_loaded.add(int(cache_rec["spoolman_id"]))
            claimants = claims.get(slot) or []
            live = [row for row in claimants if not row.get("archived")]
            for row in claimants:
                if row.get("archived"):
                    conflicts.append(
                        f"Slot {slot}: spool #{row['id']} is archived but still claims "
                        "this slot in Spoolman — run the sync to clear the claim")
            spool_view = None
            if len(live) > 1:
                ids = " and ".join(f"#{row['id']}" for row in
                                    sorted(live, key=lambda r: int(r["id"])))
                conflicts.append(
                    f"Slot {slot}: spools {ids} both claim this slot in Spoolman — "
                    "neither was picked; run the sync to name the conflict")
            elif len(live) == 1:
                spool_view = spool_summary(live[0], cache_rec)
            elif cache_rec.get("spoolman_id") is not None:
                cached = by_id.get(int(cache_rec["spoolman_id"]))
                spool_view = spool_summary(cached, cache_rec)
            slot_views.append({"slot": slot, "spool": spool_view})
        shelf = [spool_summary(row) for row in
                 sorted(spools, key=lambda r: int(r["id"]))
                 if not row.get("archived") and int(row["id"]) not in claimed_anywhere
                 and int(row["id"]) not in cache_loaded]
        all_spools = [spool_summary(row) for row in
                      sorted(spools, key=lambda r: int(r["id"]))]
    except SpoolsError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "name": entry.get("name"),
                 "model": entry.get("model"), "slots": slot_views, "shelf": shelf,
                 "spools": all_spools, "conflicts": conflicts}


# ------------------------------------------------------------------ actions

def assign(data, printer_id, body=None, *, client=None, mr=None,
           state_path=None, pending_path=None):
    """(code, body) for POST /api/printers/<id>/spools/assign.

    Body: ``{"slot": <1-4>, "spool_id": <id>}``. Through bind_slot with
    its default disposition: a replaced spool goes back on the shelf.
    """
    try:
        entry = _resolve_write(data, printer_id)
        body = _body(body)
        slot = _slot_field(body)
        spool_id = _int_field(body, "spool_id")
        result = _call(spool_sync.bind_slot, slot, spool_id,
                       client=_client(client), mr=_mr(mr, printer_id),
                       state_path=state_path, pending_path=pending_path,
                       printer_id=printer_id,
                       note="Bound from the Web UI spools page")
    except SpoolsError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": "assign", "slot": slot,
                 "spool_id": spool_id, "detail": spool_sync.bind_text(result)}


def swap(data, printer_id, body=None, *, client=None, mr=None,
         state_path=None, pending_path=None):
    """(code, body) for POST /api/printers/<id>/spools/swap.

    Body: ``{"slot_a": <1-4>, "slot_b": <1-4>}``; through swap_slots.
    """
    try:
        entry = _resolve_write(data, printer_id)
        body = _body(body)
        slot_a = _slot_field(body, "slot_a")
        slot_b = _slot_field(body, "slot_b")
        result = _call(spool_sync.swap_slots, slot_a, slot_b,
                       client=_client(client), mr=_mr(mr, printer_id),
                       state_path=state_path, pending_path=pending_path,
                       printer_id=printer_id,
                       note="Swapped from the Web UI spools page")
    except SpoolsError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": "swap",
                 "slot_a": slot_a, "slot_b": slot_b,
                 "spool_in_a": result["spool_in_a"], "spool_in_b": result["spool_in_b"],
                 "detail": spool_sync.swap_text(result)}


def unassign(data, printer_id, body=None, *, client=None, mr=None,
             state_path=None, pending_path=None):
    """(code, body) for POST /api/printers/<id>/spools/unassign.

    Body: ``{"slot": <1-4>}``; through unbind_slot (clear ph_slot +
    location Shelf + remove the cache record, Spoolman first).
    """
    try:
        entry = _resolve_write(data, printer_id)
        body = _body(body)
        slot = _slot_field(body)
        result = _call(spool_sync.unbind_slot, slot,
                       client=_client(client), mr=_mr(mr, printer_id),
                       state_path=state_path, pending_path=pending_path,
                       printer_id=printer_id,
                       note="Unbound from the Web UI spools page")
    except SpoolsError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": "unassign", "slot": slot,
                 "spool_id": result["spool_id"],
                 "detail": spool_sync.unbind_text(result)}


def add(data, printer_id, body=None, *, client=None, mr=None,
        state_path=None, pending_path=None):
    """(code, body) for POST /api/printers/<id>/spools/add.

    Body: ``{"brand": ..., "material": ..., "color": ..., "grams": ...,
    "hex": ...}`` (brand and hex optional). Creates one spool on the
    shelf through create_spool, the #512 creation path; loading it into
    a slot is a separate assign, exactly as after ``/spool new``.
    """
    try:
        entry = _resolve_write(data, printer_id)
        body = _body(body)
        material = str(body.get("material") or "").strip()
        color = str(body.get("color") or "").strip()
        grams = body.get("grams")
        if isinstance(grams, bool) or not isinstance(grams, (int, float)) or grams != grams \
                or grams <= 0:
            raise SpoolsError(400, "grams must be a positive full starting weight in grams. "
                                   "Nothing was created.")
        if not material or not color:
            raise SpoolsError(400, "material and color are required. Nothing was created.")
        from ..core import spool_canon
        if spool_canon.canonical_material(material) is None:
            raise SpoolsError(400, f"{material!r} is not a known material (pick one of "
                                   f"{', '.join(spool_canon.MATERIALS)}). Nothing was created.")
        material = spool_canon.canonical_material(material)
        hex_code = None
        if body.get("hex"):
            hex_code = spool_sync.screen_hex(body.get("hex"))
            if hex_code is None:
                raise SpoolsError(400, f"{body.get('hex')!r} is not a hex color "
                                       "(e.g. 0C6283). Nothing was created.")
        spec = {"brand": str(body.get("brand") or "").strip() or None,
                "material": material, "color": color, "grams": round(float(grams), 1),
                "hex": hex_code}
        made = _call(spool_sync.create_spool, spec, client=_client(client))
    except SpoolsError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": "add",
                 "spool_id": made["spool_id"], "filament_id": made["filament_id"],
                 "reused_filament": made["reused_filament"],
                 "detail": f"Created spool #{made['spool_id']} on the shelf"
                           f"{' (existing filament type reused)' if made['reused_filament'] else ''}. "
                           f"Load it with an assign to a slot."}


def edit(data, printer_id, body=None, *, client=None, mr=None,
         state_path=None, pending_path=None):
    """(code, body) for POST /api/printers/<id>/spools/edit.

    Body: ``{"spool_id": <id>, ...}`` plus any of ``name``, ``material``,
    ``color`` (a color name, or a hex code), ``color_hex``, ``brand``
    (or ``vendor``), ``remaining_weight`` and ``archived``. Only the
    fields present are written. Remaining weight and archived go to
    the spool record; the rest go to its filament record — the record
    Spoolman's own UI edits, shared by every spool of that filament
    type, which the response names.
    """
    try:
        entry = _resolve_write(data, printer_id)
        body = _body(body)
        spool_id = _int_field(body, "spool_id")
        client = _client(client)
        spool = _call(client, "GET", f"/api/v1/spool/{spool_id}", None)
        if not isinstance(spool, dict) or spool.get("id") is None:
            raise SpoolsError(404, f"Spoolman has no spool #{spool_id}. Nothing changed.")
        spool_patch: dict = {}
        filament_patch: dict = {}
        if "remaining_weight" in body:
            weight = body["remaining_weight"]
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
                    or weight != weight or weight < 0:
                raise SpoolsError(400, "remaining_weight must be a number of grams, "
                                       "0 or more. Nothing changed.")
            spool_patch["remaining_weight"] = float(weight)
        if "archived" in body:
            if not isinstance(body["archived"], bool):
                raise SpoolsError(400, "archived must be true or false. Nothing changed.")
            spool_patch["archived"] = body["archived"]
        for key in ("name", "material"):
            if key in body:
                text = str(body[key] or "").strip()
                if not text:
                    raise SpoolsError(400, f"{key} must not be empty. Nothing changed.")
                if key == "material":
                    from ..core import spool_canon
                    canon = spool_canon.canonical_material(text)
                    if canon is None:
                        raise SpoolsError(400, f"{text!r} is not a known material (pick one of "
                                               f"{', '.join(spool_canon.MATERIALS)}). Nothing changed.")
                    text = canon
                filament_patch[key] = text
        hex_code = None
        if "color_hex" in body and body["color_hex"] not in (None, ""):
            hex_code = spool_sync.screen_hex(body["color_hex"])
            if hex_code is None:
                raise SpoolsError(400, f"{body['color_hex']!r} is not a hex color "
                                       "(e.g. 0C6283). Nothing changed.")
        if "color" in body and body["color"] not in (None, ""):
            color_hex = spool_sync.screen_hex(body["color"])
            if color_hex is not None:
                if hex_code is not None and color_hex != hex_code:
                    raise SpoolsError(400, "color and color_hex name different color "
                                           "codes. Nothing changed.")
                hex_code = color_hex
            else:
                color_name = str(body["color"]).strip()
                if "name" in filament_patch and filament_patch["name"] != color_name:
                    raise SpoolsError(400, "name and color name different colors: in "
                                           "Spoolman the filament name is the color name, "
                                           "so give only one. Nothing changed.")
                filament_patch["name"] = color_name
        if hex_code is not None:
            filament_patch["color_hex"] = hex_code
        brand = body.get("brand", body.get("vendor"))
        if brand is not None and str(brand).strip():
            brand = str(brand).strip()
            vendors = _call(client, "GET", "/api/v1/vendor", None) or []
            vendor = next((v for v in vendors if isinstance(v, dict)
                           and str(v.get("name") or "").casefold() == brand.casefold()), None)
            if vendor is None:
                vendor = _call(client, "POST", "/api/v1/vendor", {"name": brand})
            filament_patch["vendor_id"] = vendor["id"]
        if not spool_patch and not filament_patch:
            raise SpoolsError(400, "give at least one field to change: name, material, "
                                   "color, color_hex, brand, remaining_weight or archived. "
                                   "Nothing changed.")
        filament = spool.get("filament") or {}
        filament_id = filament.get("id") or spool.get("filament_id")
        if filament_patch and filament_id is None:
            raise SpoolsError(400, f"spool #{spool_id} has no filament record to edit. "
                                   "Nothing changed.")
        if filament_patch:
            _call(client, "PATCH", f"/api/v1/filament/{int(filament_id)}", filament_patch)
        if spool_patch:
            _call(client, "PATCH", f"/api/v1/spool/{spool_id}", spool_patch)
        updated = _call(client, "GET", f"/api/v1/spool/{spool_id}", None)
    except SpoolsError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    detail = f"Spool #{spool_id} updated in Spoolman."
    if filament_patch:
        detail += (" The name, material, color and brand live on the filament record, "
                   "so every spool of that filament type shows the change.")
    return 200, {"ok": True, "id": entry["id"], "action": "edit", "spool_id": spool_id,
                 "spool": spool_summary(updated if isinstance(updated, dict) else spool),
                 "detail": detail}


def sync(data, printer_id, body=None, *, client=None, mr=None,
         state_path=None, pending_path=None):
    """(code, body) for POST /api/printers/<id>/spools/sync: reconcile."""
    try:
        entry = _resolve_write(data, printer_id)
        result = _call(spool_sync.reconcile, printer_id,
                       client=_client(client), mr=_mr(mr, printer_id),
                       state_path=state_path, pending_path=pending_path)
        if result.get("error"):
            raise SpoolsError(502, str(result["error"]))
    except SpoolsError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": "sync",
                 "assignments": result.get("assignments") or {},
                 "conflicts": result.get("conflicts") or [],
                 "detail": spool_sync.reconcile_text(result)}


# ------------------------------------------------------------------ #801 hook

def _default_ai_call():
    """The AI call for purchase intake, on the #514 vision route.

    The Web UI twin of the chat plugin's ``_purchase_ai_call``: page
    text goes to the configured chat endpoint (or PRINT_VISION_URL),
    with the listener's configured values.
    """
    import urllib.request

    from ..runtime import moonraker_event_listener as listener
    from ..runtime import visual_watch

    def call(*, prompt, image=None, text=None):
        if image is not None:
            return visual_watch.visual_extract(
                image, prompt, chat_url=listener._vision_chat_url,
                model=listener._VISION_MODEL,
                api_key=lambda: listener._profile_env_value("CLIPROXY_API_KEY"),
                context="Filament purchase screenshot or order. Reply with JSON only.")
        full = prompt if text is None else f"{prompt}\n\nPage text:\n{text}"
        endpoint = _os_environ_vision_url()
        if endpoint:
            import base64 as _b64
            payload = json.dumps({"image": _b64.b64encode(b"").decode(),
                                  "prompt": full}).encode()
            req = urllib.request.Request(endpoint, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read())
        url = listener._vision_chat_url()
        key = listener._profile_env_value("CLIPROXY_API_KEY")
        if not key and not visual_watch.keyless_ok(url):
            return None
        body = {"model": listener._VISION_MODEL, "temperature": 0, "max_tokens": 800,
                "messages": [{"role": "user", "content": full}]}
        headers = {"Content-Type": "application/json"}
        key = credential_binding.key_for("services.vision_chat_url", url, key)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read())
        content = visual_watch._strip_json_fence(result["choices"][0]["message"]["content"])
        try:
            return json.loads(content)
        except ValueError:
            return content

    return call


def _os_environ_vision_url():
    import os
    return os.environ.get("PRINT_VISION_URL")


def purchase_available(*, ai_configured=None):
    """True when the purchase-link action may be offered at all (#801's gate)."""
    check = purchase_intake.ai_route_configured if ai_configured is None else ai_configured
    try:
        return bool(check())
    except Exception:
        return False


def propose_purchase(data, printer_id, body=None, *, ai_configured=None,
                     propose=None, ai_call=None):
    """(code, body) for POST /api/printers/<id>/spools/purchase/propose.

    Body: ``{"url": <shop/order link>}``. Proposes only — creates
    nothing — and only when an AI route is configured (#801's gate);
    structured page data still resolves inside propose_from_link
    without an AI call when it is complete.
    """
    try:
        entry = _resolve_write(data, printer_id)
        body = _body(body)
        url = str(body.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise SpoolsError(400, "url must be an http(s) shop or order link. "
                                   "Nothing was created.")
        if not purchase_available(ai_configured=ai_configured):
            raise SpoolsError(403, NO_AI_REASON)
        propose = propose or purchase_intake.propose_from_link
        try:
            if propose is purchase_intake.propose_from_link:
                lines = propose(url, ai_call=ai_call or _default_ai_call())
            else:
                lines = propose(url)
        except purchase_intake.PurchaseError as refusal:
            raise SpoolsError(400, str(refusal)) from None
    except SpoolsError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": "purchase/propose",
                 "lines": lines, "detail": purchase_intake.confirm_text(lines)}


def create_purchase_spools(data, printer_id, body=None, *, client=None,
                           create_purchase=None):
    """(code, body) for POST /api/printers/<id>/spools/purchase/create.

    Body: ``{"lines": [<proposed line>, ...]}`` — the explicit confirm
    step. The lines are re-validated through purchase_intake's own
    validation before anything is created, then created on the shelf
    through create_purchase / create_spool.
    """
    try:
        entry = _resolve_write(data, printer_id)
        body = _body(body)
        raw_lines = body.get("lines")
        if not isinstance(raw_lines, list) or not raw_lines:
            raise SpoolsError(400, "lines must be the proposed purchase lines to create. "
                                   "Nothing was created.")
        purchase_url = body.get("purchase_url") or next(
            (line.get("purchase_url") for line in raw_lines if isinstance(line, dict)), None)
        try:
            lines = purchase_intake.validate_purchase_answer(
                {"items": raw_lines}, purchase_url=purchase_url)
        except purchase_intake.PurchaseError as refusal:
            raise SpoolsError(400, str(refusal)) from None
        create = create_purchase or purchase_intake.create_purchase
        try:
            if create is purchase_intake.create_purchase:
                created = create(lines, create_spool=lambda spec: spool_sync.create_spool(
                    spec, client=_client(client)))
            else:
                created = create(lines)
        except purchase_intake.PartialCreationError as partial:
            return 409, {"ok": False, "reason": str(partial),
                         "created": [{"spool_id": made["spool_id"]} for made in partial.created]}
    except SpoolsError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": "purchase/create",
                 "created": [{"spool_id": made["spool_id"],
                              "filament_id": made["filament_id"],
                              "reused_filament": made["reused_filament"]}
                             for made in created],
                 "detail": purchase_intake.created_text(created)}


def spool_action(data, printer_id, action, body=None, **deps):
    """Dispatch one POST action: (code, body), controls.py's shape."""
    handlers = {"assign": assign, "swap": swap, "unassign": unassign, "add": add,
                "edit": edit, "sync": sync, "purchase/propose": propose_purchase,
                "purchase/create": create_purchase_spools}
    handler = handlers.get(action)
    if handler is None:
        return 404, {"ok": False,
                     "reason": "try /api/printers/<id>/spools/assign, swap, unassign, "
                               "add, edit, sync, purchase/propose or purchase/create"}
    accepted = {"purchase/propose": ("ai_configured", "propose", "ai_call"),
                "purchase/create": ("client", "create_purchase")}
    keys = accepted.get(action, ("client", "mr", "state_path", "pending_path"))
    kwargs = {key: deps[key] for key in keys if key in deps}
    return handler(data, printer_id, body, **kwargs)


# ------------------------------------------------------------------ the page

def _esc(value):
    return html.escape(str(value), quote=True)


def _page(title, body, *, theme=shell.DEFAULT_THEME):
    """Through the one shell (#813); the header chrome carries the nav."""
    return shell.page(title, body, path="/spools", theme=theme)


def render_spools_index(entries, *, theme=shell.DEFAULT_THEME):
    """GET /spools: one link per configured printer — no default is picked."""
    items = "".join(
        f'<li><a href="/spools/{quote(str(entry["id"]), safe="")}">{_esc(entry.get("name") or entry["id"])}</a>'
        f' ({_esc(entry["id"])}, {_esc(entry.get("model") or "")})</li>'
        for entry in entries)
    return _page("spools", "<p>Spools are managed per printer. Pick the printer:</p>"
                           f"<ul>{items}</ul>", theme=theme)


def _amount_text(spool):
    """Grams and percent as the #744 loaded view words them."""
    grams = spool.get("remaining_weight")
    if grams is None:
        return "amount unknown"
    text = f"{grams:g} g left"
    if spool.get("remaining_percent") is not None:
        initial = spool.get("initial_weight")
        text += f" ({spool['remaining_percent']:.0f}% of {initial:g} g)"
    else:
        text += " (percent unknown: the spool's starting weight is not recorded)"
    return text


def _picker_label(spool):
    from ..runtime.discord_print_summary import spool_picker_label
    return spool_picker_label(spool.get("color"), spool.get("material"),
                              spool.get("id"), spool.get("remaining_weight"))


def _options(items, *, selected=None):
    return "".join(
        f'<option value="{_esc(value)}"{" selected" if selected is not None and str(value) == str(selected) else ""}>'
        f'{_esc(label)}</option>' for value, label in items)


_OTHER = "__other__"
_COMMON_COLORS = ("Black", "White", "Gray", "Red", "Orange", "Yellow", "Green", "Blue",
                  "Purple", "Pink", "Brown", "Beige", "Gold", "Silver", "Transparent")


def _material_select(name, *, required, blank=None):
    """Material picker: primary types as groups, their subtypes inside.

    Only canonical materials can be chosen, so the form can never mint a
    new material spelling.
    """
    from ..core import spool_canon
    head = f'<option value="">{_esc(blank)}</option>' if blank else ""
    groups = []
    for primary, subtypes in spool_canon.MATERIAL_TREE.items():
        label = spool_canon.PRIMARY_LABELS.get(primary, primary)
        inner = "".join(f'<option value="{_esc(m)}">{_esc(m if m != primary or len(subtypes) == 1 else m + " (plain)")}</option>'
                        for m in subtypes)
        groups.append(f'<optgroup label="{_esc(label)}">{inner}</optgroup>')
    return (f'<select name="{name}"{" required" if required else ""}>{head}{"".join(groups)}</select>')


def _known_select(name, values, *, blank, other_label):
    """A picker of known values plus an Other that needs a typed value and an explicit yes."""
    seen, items = set(), []
    for value in values:
        text = str(value or "").strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            items.append((text, text))
    head = f'<option value="">{_esc(blank)}</option>' if blank is not None else ""
    return (f'<select name="{name}">{head}{_options(items)}'
            f'<option value="{_OTHER}">{_esc(other_label)}</option></select>')


def _picked(first, name, what):
    """The picked value, or the typed one when Other was chosen with an explicit yes."""
    value = first(name)
    if value != _OTHER:
        return value
    typed = first(f"{name}_new")
    if not typed:
        raise SpoolsError(400, f"Other {what} was picked but nothing was typed. Nothing changed.")
    if first("confirm_new") != "true":
        raise SpoolsError(400, f"“{typed}” is not in the {what} list. Tick “Add new values” "
                               f"to add it as a new {what}. Nothing changed.")
    return typed


def _form(form_token, printer_id, action, inner, attrs=""):
    """One picker form. ``attrs`` carries extra form attributes (the
    shell script's data-confirm, #840); it is a code-side literal,
    never operator input."""
    return (f'<form method="post" action="/spools/{quote(str(printer_id), safe="")}"'
            f'{" " + attrs if attrs else ""}>'
            f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
            f'<input type="hidden" name="spool_action" value="{_esc(action)}">'
            f"{inner}</form>")


def render_spools_error(printer_id, code, reason, *, theme=shell.DEFAULT_THEME):
    """The browser error page for a failed spools view (#840): the
    shell, the plain reason, and the way forward — Configuration for a
    Spoolman outage (502), the printer picker for anything else. The
    caller keeps its status code; only the JSON API keeps the
    ``{ok, reason}`` body for this failure."""
    if code == 502:
        hint = ('Check that Spoolman is running and that the Spoolman URL on '
                '<a href="/config">Configuration</a> is right, then reload.')
    else:
        hint = '<a href="/spools">Pick a printer</a>.'
    return _page(f"spools — {printer_id}",
                 f'<p class="error">{_esc(reason)}</p><p>{hint}</p>', theme=theme)


def render_spools_page(view, *, form_token, notice=None, error=None, proposed=None,
                       purchase_ready=False, theme=shell.DEFAULT_THEME, printers=None):
    """The per-printer Spools page: the #744 loaded view, the shelf,
    and the assign/swap/unassign/add/edit/sync forms with pickers.

    ``printers`` (the configured roster) adds a printer switcher when
    there is more than one printer (#875)."""
    esc = _esc
    pid = view["id"]
    parts = []
    if printers and len(printers) > 1:
        links = " ".join(
            (f'<strong>{esc(entry.get("name") or entry["id"])}</strong>'
             if str(entry["id"]) == str(pid) else
             f'<a href="/spools/{quote(str(entry["id"]), safe="")}">'
             f'{esc(entry.get("name") or entry["id"])}</a>')
            for entry in printers)
        parts.append(f'<nav class="switcher" aria-label="Printer">Printer: {links}</nav>')
    if notice:
        parts.append(f'<p class="notice">{esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{esc(error)}</p>')
    loaded_rows = "".join(
        f"<tr><th>Slot {row['slot']}</th><td>"
        + (f"{esc(_picker_label(row['spool']))} — {esc(_amount_text(row['spool']))}"
           if row["spool"] else "empty")
        + "</td></tr>" for row in view["slots"])
    parts.append(f"<h2>Loaded</h2><table>{loaded_rows}</table>")
    if view["shelf"]:
        shelf_rows = "".join(
            f"<tr><td>{esc(_picker_label(spool))}</td><td>{esc(_amount_text(spool))}</td></tr>"
            for spool in view["shelf"])
        parts.append(f'<h2>Shelf</h2><div class="table-scroll"><table>{shelf_rows}</table></div>')
    else:
        parts.append("<h2>Shelf</h2><p>(nothing on the shelf)</p>")
    if view.get("conflicts"):
        items = "".join(f"<li>{esc(conflict)}</li>" for conflict in view["conflicts"])
        parts.append(f"<h2>Conflicts</h2><ul>{items}</ul>"
                     "<p>Run the sync below to have the conflict named and the "
                     "slot state converged from Spoolman.</p>")
    slot_options = [(row["slot"], f"Slot {row['slot']}") for row in view["slots"]]
    loaded_slot_options = [
        (row["slot"], f"Slot {row['slot']}: {_picker_label(row['spool'])}")
        for row in view["slots"] if row["spool"]]
    shelf_options = [(spool["id"], _picker_label(spool)) for spool in view["shelf"]]
    all_options = [(spool["id"], _picker_label(spool) + (" (archived)" if spool["archived"] else ""))
                   for spool in view["spools"]]
    parts.append("<h2>Assign a spool to a slot</h2>")
    if shelf_options:
        parts.append(_form(form_token, pid, "assign",
                           f'<label>Slot <select name="slot">{_options(slot_options)}</select></label> '
                           f'<label>Spool <select name="spool_id">{_options(shelf_options)}</select></label> '
                           '<button type="submit">Assign</button>'
                           "<p>A spool already in the slot goes back on the shelf.</p>"))
    else:
        parts.append("<p>Nothing on the shelf to assign. Add a spool below first.</p>")
    parts.append("<h2>Swap two slots</h2>")
    if len(loaded_slot_options) >= 1 and len(slot_options) >= 2:
        parts.append(_form(form_token, pid, "swap",
                           f'<label>First slot <select name="slot_a">{_options(loaded_slot_options)}</select></label> '
                           f'<label>Second slot <select name="slot_b">{_options(slot_options)}</select></label> '
                           '<button type="submit">Swap</button>',
                           attrs='data-confirm="Swap the spools in these two slots?"'))
    else:
        parts.append("<p>No loaded slot to swap yet.</p>")
    parts.append("<h2>Unassign a slot</h2>")
    if loaded_slot_options:
        parts.append(_form(form_token, pid, "unassign",
                           f'<label>Slot <select name="slot">{_options(loaded_slot_options)}</select></label> '
                           '<button type="submit">Unassign</button>'
                           "<p>The spool goes back on the shelf.</p>",
                           attrs='data-confirm="Unassign this slot? '
                                 'Its spool goes back on the shelf."'))
    else:
        parts.append("<p>No slot has a spool to unassign.</p>")
    from ..core import spool_canon
    known_brands = sorted({b for b in (s.get("brand") for s in view["spools"])
                           if spool_canon.canonical_brand(b)}, key=str.casefold)
    known_colors = sorted({str(s.get("color") or "").strip() for s in view["spools"]} - {""},
                          key=str.casefold)
    color_values = [*_COMMON_COLORS, *known_colors]
    new_fields = ('<label>Other brand <input name="brand_new"></label> '
                  '<label>Other color <input name="color_new"></label> '
                  '<label>Add new values <input name="confirm_new" type="checkbox" value="true"></label> ')
    parts.append("<h2>Add a spool</h2>")
    parts.append(_form(form_token, pid, "add",
                       f'<label>Material {_material_select("material", required=True)}</label> '
                       f'<label>Brand {_known_select("brand", known_brands, blank="(no brand)", other_label="Other brand…")}</label> '
                       f'<label>Color {_known_select("color", color_values, blank=None, other_label="Other color…")}</label> '
                       + new_fields +
                       '<label>Grams <input name="grams" type="number" step="any" min="1" required></label> '
                       '<label>Color code (hex, optional) <input name="hex"></label> '
                       '<button type="submit">Add to the shelf</button>'
                       "<p>Material is picked by type, then kind (PLA, PLA+, Silk, Matte). "
                       "A brand or color that is not in the list needs Other, the typed value, "
                       "and the Add new values box ticked.</p>"))
    parts.append("<h2>Edit a spool's Spoolman record</h2>")
    if all_options:
        parts.append(_form(form_token, pid, "edit",
                           f'<label>Spool <select name="spool_id">{_options(all_options)}</select></label> '
                           f'<label>Color name {_known_select("name", color_values, blank="(unchanged)", other_label="Other color…")}</label> '
                           f'<label>Material {_material_select("material", required=False, blank="(unchanged)")}</label> '
                           '<label>Color code (hex) <input name="color_hex"></label> '
                           f'<label>Brand {_known_select("brand", known_brands, blank="(unchanged)", other_label="Other brand…")}</label> '
                           '<label>Other color <input name="name_new"></label> '
                           '<label>Other brand <input name="brand_new"></label> '
                           '<label>Add new values <input name="confirm_new" type="checkbox" value="true"></label> '
                           '<label>Remaining weight (g) <input name="remaining_weight" type="number" step="any" min="0"></label> '
                           '<label>Archived <input name="archived" type="checkbox" value="true"></label> '
                           '<label>Unarchive <input name="unarchive" type="checkbox" value="true"></label> '
                           '<button type="submit">Save changes</button>'
                           "<p>Blank fields are left unchanged. Name, material, color and "
                           "brand live on the filament record, so every spool of that "
                           "filament type shows the change.</p>"))
    else:
        parts.append("<p>There are no spools to edit.</p>")
    parts.append("<h2>Sync</h2>")
    parts.append(_form(form_token, pid, "sync",
                       '<button type="submit" data-busy="Syncing…">Sync spools from Spoolman</button>'
                       "<p>Reconciles the slot state with Spoolman and the printer, "
                       "the same reconciliation the chat sync runs.</p>"))
    parts.append("<h2>Add spools from a purchase link</h2>")
    if purchase_ready:
        parts.append(_form(form_token, pid, "purchase_propose",
                           '<label>Product or order link <input name="url" size="60"></label> '
                           '<button type="submit" data-busy="Reading the purchase…">Read the purchase</button>'
                           "<p>The proposed spools are shown before anything is created.</p>"))
        if proposed is not None:
            lines = "".join(f"<li>{esc(purchase_intake.line_text(line))}</li>"
                            for line in proposed)
            hidden = esc(json.dumps(proposed))
            parts.append(
                f"<p>Here is what was read from that purchase:</p><ul>{lines}</ul>"
                + _form(form_token, pid, "purchase_create",
                        f'<input type="hidden" name="purchase_lines" value="{hidden}">'
                        '<button type="submit" data-busy="Creating…">Create these spools</button>'
                        "<p>Nothing is created until you press Create.</p>"))
    else:
        parts.append(f"<p>{esc(NO_AI_REASON)}</p>")
    return _page(f"spools — {view.get('name') or pid}", "".join(parts), theme=theme)


# ------------------------------------------------------------------ form posts

_PAGE_ACTIONS = {"assign": "assign", "swap": "swap", "unassign": "unassign",
                 "add": "add", "edit": "edit", "sync": "sync",
                 "purchase_propose": "purchase/propose",
                 "purchase_create": "purchase/create"}


def form_body(action, form):
    """(api_action, body) for one Spools page form post.

    ``form`` is a parse_qs mapping. Pickers submit ids and slot numbers
    as text; they are converted here, and a value that is not a whole
    number is a named refusal, never a silent default. Edit fields left
    blank are omitted (left unchanged), the form's own rule.
    """
    api_action = _PAGE_ACTIONS.get(action)
    if api_action is None:
        raise SpoolsError(400, f"unknown spools form action {action!r}. Nothing changed.")

    def first(name):
        values = form.get(name) or [""]
        return str(values[0]).strip()

    def number(name, *, integer=False):
        raw = first(name)
        try:
            value = int(raw) if integer else float(raw)
        except ValueError:
            raise SpoolsError(400, f"{name} must be a number. Nothing changed.") from None
        return value

    if api_action == "assign":
        return api_action, {"slot": number("slot", integer=True),
                            "spool_id": number("spool_id", integer=True)}
    if api_action == "swap":
        return api_action, {"slot_a": number("slot_a", integer=True),
                            "slot_b": number("slot_b", integer=True)}
    if api_action == "unassign":
        return api_action, {"slot": number("slot", integer=True)}
    if api_action == "add":
        body = {"brand": _picked(first, "brand", "brand") or None, "material": first("material"),
                "color": _picked(first, "color", "color"), "grams": number("grams")}
        if first("hex"):
            body["hex"] = first("hex")
        return api_action, body
    if api_action == "edit":
        body: dict = {"spool_id": number("spool_id", integer=True)}
        picked = {"name": _picked(first, "name", "color"), "material": first("material"),
                  "color_hex": first("color_hex"), "brand": _picked(first, "brand", "brand")}
        for name, value in picked.items():
            if value:
                body[name] = value
        if first("remaining_weight"):
            body["remaining_weight"] = number("remaining_weight")
        # The checkbox only submits when ticked, and edit cannot
        # unarchive from this form without a value: an unticked box on a
        # live spool must not archive it, so only "true" is forwarded.
        if first("archived") == "true":
            body["archived"] = True
        elif first("unarchive") == "true":
            body["archived"] = False
        return api_action, body
    if api_action == "sync":
        return api_action, {}
    if api_action == "purchase/propose":
        return api_action, {"url": first("url")}
    # purchase/create: the proposed lines ride back as JSON in one
    # hidden field — the explicit confirm of exactly what was shown.
    try:
        lines = json.loads(first("purchase_lines") or "[]")
    except ValueError:
        raise SpoolsError(400, "the proposed purchase lines could not be read back. "
                               "Nothing was created.") from None
    return api_action, {"lines": lines}


__all__ = [
    "ACTIONS", "NO_AI_REASON", "PURCHASE_ACTIONS", "SpoolsError", "add", "assign",
    "create_purchase_spools", "edit", "form_body", "propose_purchase",
    "purchase_available", "render_spools_error", "render_spools_index",
    "render_spools_page", "spool_action", "spool_summary", "spools_view", "swap",
    "sync", "unassign",
]
