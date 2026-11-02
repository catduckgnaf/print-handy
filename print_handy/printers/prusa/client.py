"""PrusaLink LAN transport: HTTP REST on port 80, the API Prusa ships on the printer itself.

Protocol facts are from Prusa's published PrusaLink OpenAPI specification (the `prusa-link`
repository, `prusalink-openapi.yaml`, v1) and the printer's own behaviour as mapped by maintained
clients; endpoint shapes are also cross-checked against vmhomelab/printbuddy's prusalink provider
(a separate AGPL-3.0 project — protocol facts only, this client is original code):

  - `GET /api/v1/info`, `GET /api/v1/status`, `GET /api/v1/job` (204 when no job) for status;
  - `GET /api/v1/storage` to discover storage (USB preferred — current firmware exposes
    printable files there; `local` is the fallback);
  - `PUT /api/v1/files/<storage>/<path>` uploads (and creates folders); a follow-up GET of the
    file must report the same size before the upload counts as verified;
  - `POST /api/v1/files/<storage>/<path>` starts the uploaded file;
  - `PUT /api/v1/job/<id>/pause`, `PUT /api/v1/job/<id>/resume`, `DELETE /api/v1/job/<id>` control;
  - `GET /api/v1/cameras` lists the cameras PrusaLink serves for the printer and
    `GET /api/v1/cameras/<id>/snap` returns the latest still (PNG; 204 when no image
    is available yet, 304 when it has not changed) — see printers/prusa/camera.py.

Auth: the PrusaLink API key travels in the `X-Api-Key` header. Firmware that answers 401 with a
Digest challenge gets one RFC 7616 (MD5) digest retry as user `maker` with the same key as the
password — the two forms PrusaLink accepts for the same secret. The key itself never appears in
an error message, a URL, or a log line.

Both the opener and the storage choice are injectable so tests run fully offline against a fake,
following the OctoPrint client's pattern. Mutations are never retried automatically, except the
single digest-auth retry of the identical request.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API = "/api/v1"


class PrusaLinkError(RuntimeError):
    """A PrusaLink failure, with no credential or payload detail attached."""


class _Response:
    """The slice of an HTTP response the client uses (a fake opener returns this shape)."""

    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.body = body if isinstance(body, bytes) else str(body).encode()

    def json(self):
        return json.loads(self.body) if self.body else {}


def _as_response(raw):
    if isinstance(raw, _Response):
        return raw
    status = getattr(raw, "status", None) or getattr(raw, "code", 200)
    headers = getattr(raw, "headers", {}) or {}
    body = raw.read() if hasattr(raw, "read") else b""
    return _Response(status, dict(headers.items()) if hasattr(headers, "items") else headers, body)


class PrusaLinkClient:
    def __init__(self, host, api_key, *, opener=None, username="maker"):
        host = str(host or "").strip()
        if not host or "://" in host or "/" in host or " " in host:
            raise PrusaLinkError("PrusaLink requires a plain printer IP address or hostname (no scheme, port or path)")
        if not api_key:
            raise PrusaLinkError("PrusaLink API key is required; supply it through the environment variable "
                                 "[prusa] api_key_env names (default PRUSA_LINK_API_KEY)")
        self.base = f"http://{host}"
        self.api_key = api_key
        self.username = username
        self.opener = opener or urlopen
        self._storage = None

    # -- transport ---------------------------------------------------------
    def _send(self, method, path, body=None, headers=None):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        req = Request(self.base + path, data=data, method=method,
                      headers={"X-Api-Key": self.api_key, **(headers or {})})
        try:
            with self.opener(req, timeout=30) as raw:
                return _as_response(raw)
        except HTTPError as exc:  # urlopen raises for non-2xx; the fake opener returns _Response instead
            payload = exc.read() if hasattr(exc, "read") else b""
            return _Response(exc.code, dict(exc.headers.items()) if exc.headers else {}, payload)
        except (URLError, OSError, TimeoutError) as exc:
            raise PrusaLinkError(f"PrusaLink {method} {path.split('?')[0]} failed; not retried") from exc

    def request(self, method, path, body=None, headers=None):
        """One API request with the API-key header; one Digest retry on a 401 Digest challenge."""
        resp = self._send(method, path, body, headers)
        if resp.status == 401 and "digest" in resp.headers.get("www-authenticate", "").lower():
            auth = self._digest(method, path, resp.headers["www-authenticate"])
            if auth:
                resp = self._send(method, path, body, {**(headers or {}), "Authorization": auth})
        if resp.status >= 400:
            raise PrusaLinkError(f"PrusaLink {method} {path.split('?')[0]} answered {resp.status}; "
                                 "not retried" if method != "GET" else
                                 f"PrusaLink {method} {path.split('?')[0]} answered {resp.status}")
        return resp

    def _digest(self, method, uri, challenge):
        params = {}
        for part in challenge.split("Digest", 1)[-1].split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip().strip('"')
        realm, nonce = params.get("realm", ""), params.get("nonce", "")
        if not nonce:
            return None
        qop = params.get("qop", "")
        nc, cnonce = "00000001", secrets.token_hex(8)
        ha1 = hashlib.md5(f"{self.username}:{realm}:{self.api_key}".encode()).hexdigest()
        if qop:
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
            return (f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
                    f'qop={qop}, nc={nc}, cnonce="{cnonce}", response="{response}"')
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return (f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
                f'response="{response}"')

    def get(self, path):
        resp = self.request("GET", path)
        if resp.status == 204:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            # Whatever answered is not speaking PrusaLink JSON (a captive
            # portal, a different service on the host); keep it inside the
            # client's own error type so callers see one failure shape.
            raise PrusaLinkError(f"PrusaLink GET {path.split('?')[0]} answered "
                                 "malformed JSON") from exc

    # -- status ------------------------------------------------------------
    def info(self):
        return self.get(f"{API}/info")

    def status(self):
        return self.get(f"{API}/status")

    def job(self):
        """The current job dict, or {} when the printer has none (204)."""
        return self.get(f"{API}/job")

    def snapshot(self):
        return {"info": self.info(), "status": self.status(), "job": self.job()}

    # -- camera --------------------------------------------------------------
    def cameras(self):
        """The cameras PrusaLink serves for this printer: a list of camera dicts.

        Each dict carries `camera_id` and a `connected` flag. Firmware
        without the camera API answers 404, which request() raises as
        PrusaLinkError like any other refusal; printers/prusa/camera.py
        treats that answer as "no camera source", never as a print
        failure. The list may arrive bare (the published spec's shape)
        or wrapped; both are normalised to the bare list.
        """
        data = self.get(f"{API}/cameras")
        if isinstance(data, dict):
            data = data.get("camera_list") or data.get("cameras") or []
        if not isinstance(data, list):
            return []
        return [camera for camera in data if isinstance(camera, dict)]

    def camera_snapshot(self, camera_id=None):
        """The latest camera still (PNG bytes), or None when there is none.

        None is a normal answer, not a failure: 204 means the camera has
        produced no image yet and 304 means the image has not changed
        since the previous poll. Transport and auth failures raise
        PrusaLinkError like every other read.
        """
        if camera_id:
            path = f"{API}/cameras/{quote(str(camera_id), safe='')}/snap"
        else:
            path = f"{API}/cameras/snap"
        resp = self.request("GET", path)
        return resp.body if resp.status == 200 else None

    # -- files ---------------------------------------------------------------
    def storage(self):
        """The writable storage key uploads go to: USB when available, else the first writable one."""
        if self._storage:
            return self._storage
        found = []
        try:
            data = self.get(f"{API}/storage")
            found = [s for s in (data.get("storage_list") or []) if isinstance(s, dict)]
        except PrusaLinkError:
            found = []
        writable = [str(s.get("path") or s.get("name") or "").strip("/") for s in found
                    if s.get("available") and not s.get("read_only")]
        writable = [w for w in writable if w]
        self._storage = next((w for w in writable if w.lower() == "usb"), writable[0] if writable else "usb")
        return self._storage

    def _file_path(self, remote_name, storage=None):
        name = Path(str(remote_name)).name
        if not name or name != str(remote_name).strip("/") or any(c in name for c in "\r\n"):
            raise PrusaLinkError(f"invalid PrusaLink remote file name {remote_name!r}")
        return f"{API}/files/{quote(storage or self.storage(), safe='')}/{quote(name, safe='')}"

    def file_info(self, remote_name, storage=None):
        """The file's metadata dict from the storage listing, or None when it is not there."""
        storage = storage or self.storage()
        data = self.get(f"{API}/files/{quote(storage, safe='')}")
        for child in data.get("children") or []:
            if isinstance(child, dict) and (child.get("name") == remote_name or
                                            child.get("display_name") == remote_name):
                return child
        return None

    def upload(self, local_path, remote_name=None):
        """PUT one sliced file, then verify the printer reports the same size. Starts nothing."""
        local = Path(local_path)
        remote = remote_name or local.name
        if local.suffix.lower() not in (".gcode", ".bgcode"):
            raise PrusaLinkError(f"{local.name}: PrusaLink takes .gcode or .bgcode files; slice the job first")
        payload = local.read_bytes()
        self.request("PUT", self._file_path(remote), payload,
                     headers={"Content-Type": "application/octet-stream", "Print-After-Upload": "?0"})
        info = self.file_info(remote)
        size = (info or {}).get("size")
        if size != len(payload):
            raise PrusaLinkError(f"upload of {remote} could not be verified (printer reports size {size!r}, "
                                 f"sent {len(payload)}); nothing was started")
        return remote

    def start_print(self, remote_name):
        """POST the verified uploaded file to start it. Gates live in the print path, never here."""
        self.request("POST", self._file_path(remote_name))
        return remote_name

    def control(self, action, job_id=None):
        """Pause, resume or stop the current job. The job id comes from the job snapshot."""
        if action not in ("pause", "resume", "stop"):
            raise PrusaLinkError(f"{action} is unsupported by the PrusaLink integration")
        if job_id is None:
            job_id = (self.job() or {}).get("id")
        if job_id is None:
            raise PrusaLinkError(f"no current job to {action}")
        if action == "stop":
            self.request("DELETE", f"{API}/job/{job_id}")
        else:
            self.request("PUT", f"{API}/job/{job_id}/{action}")
        return True
