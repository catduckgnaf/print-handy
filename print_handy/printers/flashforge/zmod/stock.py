"""AD5X stock firmware /detail: native IFS occupancy and the native start gate.

Moved out of core.model_job; model_job keeps thin delegators so the call sites and test seams stay.
Serial and check code come from the environment or the profile .env, and are never logged.
"""
import os
import urllib.parse

from .... import settings

STOCK_CRED_KEYS = ("AD5X_SERIAL", "AD5X_CHECK_CODE")


def _slots():
    return settings.current()["printer"]["filament_slots"]


def _request_json(*args, **kwargs):
    from ....core import model_job
    return model_job.request_json(*args, **kwargs)


def _stock_credentials():
    """Serial + LAN check code from env, else the 3dprinting profile .env. Never logged."""
    found = {k: os.environ.get(k, "").strip() for k in STOCK_CRED_KEYS}
    env_file = settings.current()["paths"]["state_home"] / ".env"
    if not all(found.values()) and env_file.is_file():
        for line in env_file.read_text(errors="ignore").splitlines():
            key, sep, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            if sep and key in found and not found[key]:
                found[key] = value.strip().strip("'\"")
    if not all(found.values()):
        missing = ", ".join(k for k, v in found.items() if not v)
        raise RuntimeError(f"stock IFS credentials missing ({missing}); set them in {env_file}")
    return found["AD5X_SERIAL"], found["AD5X_CHECK_CODE"]


def read_stock_detail(state, *, post=None):
    """Read native status using profile credentials without logging them."""
    host = urllib.parse.urlsplit(state["moonraker_url"]).hostname
    serial, code = _stock_credentials()
    post = post or (lambda url, body: _request_json(url, data=body, headers={"Content-Type": "application/json"}, timeout=10))
    reply = post(f"http://{host}:{settings.current()['services']['stock_api_port']}/detail",
                 {"serialNumber": serial, "checkCode": code})
    if not isinstance(reply, dict) or reply.get("code") != 0:
        msg = reply.get("message") if isinstance(reply, dict) else "invalid reply"
        raise RuntimeError(f"stock IFS status refused: {msg}")
    detail = reply.get("detail")
    if not isinstance(detail, dict):
        raise RuntimeError("stock firmware detail unavailable")
    return detail


def validate_native_start(detail, selection):
    """Require native ready and an explicit physical-slot route, not stale feeder state."""
    if detail.get('status') != 'ready':
        raise RuntimeError(f"native printer is not ready ({detail.get('status')!r})")
    slot = selection.get('slot') if isinstance(selection, dict) else None
    if type(slot) is not int or not 1 <= slot <= _slots():
        raise RuntimeError(f"selected physical slot is invalid ({slot!r})")
    if detail.get('hasMatlStation') is not True:
        raise RuntimeError("native IFS attachment state is unknown or absent")
    station = detail.get('matlStationInfo')
    slots = station.get('slotInfos') if isinstance(station, dict) else None
    matches = [item for item in slots if isinstance(item, dict) and item.get('slotId') == slot] if isinstance(slots, list) else []
    if len(matches) != 1 or matches[0].get('hasFilament') is not True:
        raise RuntimeError(f"native physical slot {slot} presence is unknown or empty")
    # currentLoadSlot is telemetry about the presently engaged feeder. The
    # generated single-tool marker does not command a physical feeder change.
    # Until a verified native selector exists, mismatch cannot be started safely.
    loaded = station.get('currentLoadSlot')
    if type(loaded) is not int or not 1 <= loaded <= _slots():
        raise RuntimeError(f"native current physical slot is unknown ({loaded!r})")
    if loaded != slot:
        raise RuntimeError(f"native physical slot {loaded} is engaged; selected slot {slot} has no verified automatic route")


def read_zmod_color_slots(status):
    """{slot: bool | None} from Klipper's zmod_color, the shape read_stock_filament_slots_from_detail returns.

    In screenless mode (display_off=1) the stock firmware is not running, so its
    /detail API refuses and ZMod's zmod_color.slots[].hasFilament (1-based IDs)
    is the live presence. A slot whose hasFilament is not a boolean is unknown.
    """
    slots = ((status or {}).get("zmod_color") or {}).get("slots")
    if not isinstance(slots, list) or not slots:
        raise RuntimeError("zmod_color slot list unavailable")
    result = {}
    for item in slots:
        raw = item.get("ID") if isinstance(item, dict) else None
        sid = int(raw) if isinstance(raw, (int, str)) and str(raw).isdigit() else None
        if sid is None or not 1 <= sid <= _slots() or sid in result:
            raise RuntimeError("zmod_color slot list malformed")
        present = item.get("hasFilament")
        result[sid] = present if isinstance(present, bool) else None
    return result


def read_stock_filament_slots_from_detail(detail):
    """Extract native slot presence from one already-fetched authenticated detail sample."""
    if detail.get("hasMatlStation") is not True:
        raise RuntimeError("stock firmware does not report an attached IFS")
    slots = (detail.get("matlStationInfo") or {}).get("slotInfos")
    if not isinstance(slots, list):
        raise RuntimeError("stock IFS slot list unavailable")
    result = {}
    for item in slots:
        raw = item.get("slotId") if isinstance(item, dict) else None
        sid = int(raw) if isinstance(raw, (int, str)) and str(raw).isdigit() else None
        if sid is None or not 1 <= sid <= _slots() or sid in result:
            raise RuntimeError("stock IFS slot list malformed")
        result[sid] = item.get("hasFilament") if isinstance(item.get("hasFilament"), bool) else None
    if not result:
        raise RuntimeError("stock IFS slot list empty")
    expected = set(range(1, _slots() + 1))
    if set(result) != expected:
        raise RuntimeError("stock IFS slot list incomplete")
    return result


def read_stock_filament_slots(state, *, post=None):
    """Native presence, not the screen-mode hardcoded zmod_color flags."""
    return read_stock_filament_slots_from_detail(read_stock_detail(state, post=post))


def apply_stock_presence(status, stock):
    """Replace zmod_color hasFilament with stock presence; unknown stays unknown."""
    color = status.get("zmod_color") if isinstance(status, dict) else None
    if isinstance(stock, dict) and stock.get("_incomplete") is True:
        raise RuntimeError("stock IFS detail sample is incomplete; refusing authoritative presence")
    slots = color.get("slots") if isinstance(color, dict) else None
    if not isinstance(slots, list):
        return status
    for item in slots:
        raw = item.get("ID") if isinstance(item, dict) else None
        sid = int(raw) if isinstance(raw, (int, str)) and str(raw).isdigit() else None
        item["hasFilament"] = stock.get(sid)
        item["presence_source"] = "stock /detail"
    return status
