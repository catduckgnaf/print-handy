"""OctoPrint REST transport. No automatic connection, retries, or end G-code."""
from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class OctoPrintError(RuntimeError):
    pass


class OctoPrintClient:
    def __init__(self, url, api_key, *, opener=None):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            raise OctoPrintError('OctoPrint requires an http(s) URL without credentials, query, or fragment')
        if not api_key:
            raise OctoPrintError('OctoPrint API key is required')
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.opener = opener or urlopen

    def request(self, method, path, body=None, *, content_type='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        req = Request(self.url + path, data=data, method=method,
                      headers={'X-Api-Key': self.api_key, 'Content-Type': content_type})
        try:
            with self.opener(req, timeout=30) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except Exception as exc:
            # Do not leak API keys, server responses, or retry an ambiguous mutation.
            raise OctoPrintError(f'OctoPrint {method} {path.split("?")[0]} failed; not retried') from exc

    def status(self):
        printer = self.request('GET', '/api/printer')
        job = self.request('GET', '/api/job')
        return {'printer': printer, 'job': job}

    @staticmethod
    def require_ready(snapshot):
        flags = (snapshot.get('printer', {}).get('state') or {}).get('flags') or {}
        if (flags.get('operational') is not True or flags.get('ready') is not True
                or any(flags.get(k) for k in ('printing', 'paused', 'pausing', 'cancelling', 'error', 'closedOrError'))):
            raise OctoPrintError('OctoPrint printer is not idle and ready; nothing started')

    def upload(self, path, filename):
        if not filename or Path(filename).name != filename or any(c in filename for c in '\r\n"\\'):
            raise OctoPrintError('invalid OctoPrint upload filename')
        boundary = 'PrintHandy' + uuid.uuid4().hex
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + Path(path).read_bytes()
        data += f'\r\n--{boundary}--\r\n'.encode()
        result = self.request('POST', '/api/files/local', data, content_type=f'multipart/form-data; boundary={boundary}')
        local = (result.get('files') or {}).get('local') or {}
        remote = local.get('path')
        if local.get('origin') != 'local' or remote != filename:
            raise OctoPrintError('OctoPrint upload target mismatch; nothing started')
        return remote

    def start(self, filename, bed_temp, nozzle_temp):
        temps = []
        for value in (bed_temp, nozzle_temp):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 300:
                raise OctoPrintError('invalid temperature target; nothing started')
            temps.append(value)
        self.require_ready(self.status())
        # Queue blocking waits before file selection/start on OctoPrint's serial queue.
        self.request('POST', '/api/printer/command', {'commands': [f'M190 S{temps[0]:g}', f'M109 S{temps[1]:g}']})
        self.request('POST', '/api/files/local/' + quote(filename, safe=''), {'command': 'select', 'print': True})

    def timelapse_movies(self):
        """The rendered timelapse movies OctoPrint reports, as served.

        OctoPrint renders a print's timelapse itself (when its timelapse
        feature is enabled) and lists the finished movies at
        GET /api/timelapse; each entry carries at least name and url.
        """
        result = self.request('GET', '/api/timelapse')
        movies = result.get('movies') if isinstance(result, dict) else None
        return movies if isinstance(movies, list) else []

    def download(self, path):
        """Raw bytes of a server-relative path (e.g. a movie's url)."""
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//'):
            raise OctoPrintError('OctoPrint download path must be server-relative')
        req = Request(self.url + path, method='GET', headers={'X-Api-Key': self.api_key})
        try:
            with self.opener(req, timeout=120) as response:
                return response.read()
        except Exception as exc:
            # Same no-leak, no-retry posture as request().
            raise OctoPrintError(f'OctoPrint GET {path.split("?")[0]} failed; not retried') from exc

    def control(self, action):
        bodies = {'pause': {'command': 'pause', 'action': 'pause'},
                  'resume': {'command': 'pause', 'action': 'resume'}, 'cancel': {'command': 'cancel'}}
        if action not in bodies:
            raise OctoPrintError(f'{action} is unsupported by OctoPrint integration')
        return self.request('POST', '/api/job', bodies[action])

    def set_targets(self, hotend_c=None, bed_c=None):
        """Set the tool0 / bed target temperatures (OctoPrint's printer API).

        One target command per heater actually requested; a value of 0
        turns that heater off. Validation matches start(): a finite
        number in range, or nothing is sent at all.
        """
        if hotend_c is None and bed_c is None:
            raise OctoPrintError('no temperature target given; nothing sent')
        for value in (hotend_c, bed_c):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or not 0 <= value <= 300):
                raise OctoPrintError('invalid temperature target; nothing sent')
        if hotend_c is not None:
            self.request('POST', '/api/printer/tool',
                         {'command': 'target', 'targets': {'tool0': hotend_c}})
        if bed_c is not None:
            self.request('POST', '/api/printer/bed',
                         {'command': 'target', 'target': bed_c})
