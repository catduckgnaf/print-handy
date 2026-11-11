"""Flashforge stock LAN transport: HTTP on port 8898, never the printer itself.

Protocol facts are from the public Flashforge API docs for the Adventurer 5M series
(Parallel-7/flashforge-api-docs, firmware 3.2.7 and 5.1.8, and GhostTypes/ff-5mp-api):
every request is a POST carrying serialNumber and checkCode, /detail returns status,
/uploadGcode takes one multipart file, and /printGcode starts a file already on the
printer. A non-zero code is a refusal. Code 2 means the printer is busy. On the AD5X,
/printGcode names the IFS bay a tool feeds from with useMatlStation, gcodeToolCnt and
materialMappings (toolId 0-based, slotId 1-based, #RRGGBB colors), per the AD5X
endpoint schema (Parallel-7/flashforge-api-docs, firmware 1.2.1); not yet verified
on a real printer.

This client is dumb transport. Occupancy, the bed-clear approval, and the submission
lease live in the print path, never here. upload() never starts a print: print=false
is sent and the response is checked for it. The check code is sent in the body because
the firmware requires it there; it is never put in a URL, a log line, or an exception.
Tests inject `post` so nothing here opens a socket.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

STOCK_PORT = 8898
DETAIL_PATH = "/detail"
CONTROL_PATH = "/control"
UPLOAD_PATH = "/uploadGcode"
PRINT_PATH = "/printGcode"
# Firmware code: the printer is already doing something. See ff-5mp-api FlashNetwork.h.
BUSY_CODE = 2
# /detail status values the public docs treat as free to take a new job.
IDLE_STATUSES = ("ready", "completed", "cancel")


class StockLanError(RuntimeError):
    """A stock LAN failure. The message never carries the check code or a response body."""


def _credentials():
    """(serial, check code) from the environment. Missing values raise before any request."""
    serial = os.environ.get("AD5X_SERIAL", "").strip()
    code = os.environ.get("AD5X_CHECK_CODE", "").strip()
    if not serial or not code:
        raise StockLanError("stock LAN credentials missing (AD5X_SERIAL, AD5X_CHECK_CODE)")
    return serial, code


def _endpoint(host, path):
    if not host or any(mark in host for mark in ("://", "/", "?", "#", " ")):
        raise StockLanError("stock LAN host must be a plain IP or hostname")
    return f"http://{host}:{STOCK_PORT}{path}"


class StockClient:
    """One printer. `post(url, body, content_type)` returns the parsed JSON object."""

    def __init__(self, host, *, post=None):
        self.host = str(host or "").strip()
        self._post = post or _urlopen_post

    def _send(self, path, body, content_type="application/json"):
        try:
            reply = self._post(_endpoint(self.host, path), body, content_type)
        except StockLanError:
            raise
        except Exception:  # noqa: BLE001 — the original can carry the check code in its body
            # Any transport or parse failure becomes our own error. The original exception
            # can carry the request body, so it is not chained.
            raise StockLanError(f"stock LAN {path} failed") from None
        if not isinstance(reply, dict) or reply.get("code") != 0:
            raise StockLanError(f"stock LAN {path} refused")
        return reply

    def detail(self):
        """The /detail object. Raises when the printer refuses or the payload has no detail."""
        serial, code = _credentials()
        reply = self._send(DETAIL_PATH, {"serialNumber": serial, "checkCode": code})
        detail = reply.get("detail")
        if not isinstance(detail, dict):
            raise StockLanError("stock LAN /detail refused")
        return detail

    def control(self, cmd, args=None):
        """One /control command ({payload: {cmd, args}}). A non-zero code is a refusal.

        Dumb transport like the rest of this client: which commands exist and
        what they actuate is the caller's protocol knowledge (see camera.py
        for streamCtrl_cmd). The check code rides in the body, as always.
        """
        serial, code = _credentials()
        body = {"serialNumber": serial, "checkCode": code,
                "payload": {"cmd": str(cmd), "args": dict(args or {})}}
        return self._send(CONTROL_PATH, body)

    def upload(self, path, filename):
        """Upload one .gcode and return its remote name. Never starts it (print=false)."""
        if not filename or Path(filename).name != filename or not filename.endswith(".gcode"):
            raise StockLanError("invalid stock upload filename")
        if any(mark in filename for mark in ("\r", "\n", '"', "\\")):
            raise StockLanError("invalid stock upload filename")
        serial, code = _credentials()
        data = Path(path).read_bytes()
        boundary = "PrintHandy" + uuid.uuid4().hex
        # gcodeFile is the field the public upload route documents; print stays false so
        # an upload can never be the thing that starts the job.
        preamble = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="serialNumber"\r\n\r\n{serial}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="checkCode"\r\n\r\n{code}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="print"\r\n\r\nfalse\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="gcodeFile"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        payload = preamble + data + f"\r\n--{boundary}--\r\n".encode()
        reply = self._send(UPLOAD_PATH, payload, content_type=f"multipart/form-data; boundary={boundary}")
        if reply.get("print") not in (False, "false", None):
            raise StockLanError("stock LAN upload returned print enabled; not used")
        remote = reply.get("fileName") or reply.get("gcodeFile") or filename
        if remote != filename:
            raise StockLanError("stock LAN upload name mismatch")
        return filename

    def print_gcode(self, filename, leveling=True, bay=None):
        """Start a file already on the printer, fed from one IFS bay when ``bay`` is given.

        ``bay`` is the T0 -> slot mapping (``material_mapping``); with it the start
        carries useMatlStation, gcodeToolCnt 1 and that one mapping, so the printer
        is told which bay feeds the print instead of choosing for itself (#1147).
        """
        if not filename or Path(filename).name != filename:
            raise StockLanError("invalid stock print filename")
        serial, code = _credentials()
        body = {
            "serialNumber": serial,
            "checkCode": code,
            "fileName": filename,
            "levelingBeforePrint": bool(leveling),
        }
        if bay is not None:
            body.update(useMatlStation=True, gcodeToolCnt=1, materialMappings=[_checked_mapping(bay)])
        self._send(PRINT_PATH, body)
        return filename

    @staticmethod
    def material_mapping(slot, *, material, tool_color, slot_color):
        """The one /printGcode materialMappings entry for T0 fed from IFS ``slot`` (1-based)."""
        return _checked_mapping({"toolId": 0, "slotId": slot, "materialName": material,
                                 "toolMaterialColor": tool_color, "slotMaterialColor": slot_color})

    @staticmethod
    def slot_info(detail, slot):
        """(materialName, materialColor as #RRGGBB or None) /detail reports for one IFS bay."""
        station = (detail or {}).get("matlStationInfo") if isinstance(detail, dict) else None
        for item in (station.get("slotInfos") if isinstance(station, dict) else None) or ():
            if isinstance(item, dict) and item.get("slotId") == slot:
                name = str(item.get("materialName") or "").strip()
                return name, _hex_color(item.get("materialColor"))
        return "", None

    @staticmethod
    def status_of(detail):
        """A /detail dict reduced to {state, filename, progress}. Unknown shapes stay unknown."""
        detail = detail if isinstance(detail, dict) else {}
        raw = str(detail.get("status") or "").strip().lower()
        state = raw if raw in ("ready", "completed", "cancel", "busy", "printing", "pause", "pausing", "heating", "error") else "unknown"
        if state == "ready":
            state = "standby"
        elif state == "completed":
            state = "complete"
        elif state == "cancel":
            state = "cancelled"
        elif state in ("pause", "pausing"):
            state = "paused"
        elif state == "heating":
            # Heating is a job in progress. Treating it as unknown let a second start through.
            state = "printing"
        filename = str(detail.get("printFileName") or detail.get("filename") or "")
        progress = detail.get("printProgress")
        if not isinstance(progress, (int, float)):
            progress = None
        return {"state": state, "filename": filename, "progress": progress}

    @staticmethod
    def slots_of(detail):
        """IFS presence from one /detail object: {slot_id: True, False, or None}.

        None means the bay was listed but hasFilament was not a boolean. Raises
        StockLanError when the station is absent or the slot list is missing,
        empty, or malformed. Does not open a socket.
        """
        detail = detail if isinstance(detail, dict) else {}
        if detail.get("hasMatlStation") is not True:
            raise StockLanError("stock firmware does not report an attached IFS")
        station = detail.get("matlStationInfo")
        slots = station.get("slotInfos") if isinstance(station, dict) else None
        if not isinstance(slots, list) or not slots:
            raise StockLanError("stock IFS slot list unavailable")
        result = {}
        for item in slots:
            raw = item.get("slotId") if isinstance(item, dict) else None
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or raw in result:
                raise StockLanError("stock IFS slot list malformed")
            present = item.get("hasFilament")
            result[raw] = present if isinstance(present, bool) else None
        return result


_HEX = "0123456789ABCDEF"


def _hex_color(value):
    """'#RRGGBB' (upper case) from a color string, or None when it is not one."""
    text = str(value or "").split(";", 1)[0].strip().upper()
    if not text.startswith("#"):
        text = "#" + text
    if len(text) == 9:  # #RRGGBBAA: the alpha is not part of the mapping
        text = text[:7]
    return text if len(text) == 7 and all(c in _HEX for c in text[1:]) else None


def _checked_mapping(mapping):
    """A materialMappings entry the firmware schema accepts, else StockLanError (nothing is sent)."""
    if not isinstance(mapping, dict):
        raise StockLanError("stock IFS bay mapping malformed")
    slot, tool = mapping.get("slotId"), mapping.get("toolId")
    if type(slot) is not int or not 1 <= slot <= 4 or tool != 0:
        raise StockLanError("stock IFS bay mapping must be T0 to a slot from 1 through 4")
    material = str(mapping.get("materialName") or "").strip()
    if not material or any(mark in material for mark in ('"', "\\", "\r", "\n")):
        raise StockLanError("stock IFS bay mapping needs a material name")
    colors = {key: _hex_color(mapping.get(key)) for key in ("toolMaterialColor", "slotMaterialColor")}
    if None in colors.values():
        raise StockLanError("stock IFS bay mapping needs #RRGGBB colors")
    return {"toolId": 0, "slotId": slot, "materialName": material, **colors}


def _urlopen_post(url, body, content_type):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = Request(url, data=data, method="POST", headers={"Content-Type": content_type})
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read()
        return json.loads(payload) if payload else {}
    except HTTPError as exc:
        exc.read()  # drain so the message we raise cannot include the body either
        raise StockLanError(f"stock LAN {url.rsplit('/', 1)[-1]} failed") from None
    except Exception:  # noqa: BLE001 — a JSON error can carry the response body
        raise StockLanError(f"stock LAN {url.rsplit('/', 1)[-1]} failed") from None
