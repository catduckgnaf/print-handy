"""Proof that the standalone bot started, without Hermes: a heartbeat file and an optional HTTP check.

The running bot rewrites <state>/bot_health.json every few seconds. ``python3 -m print_handy.bot health``
exits 0 only when that heartbeat is fresh, the bot is logged in to Discord and its process is alive. With [bot] health_port set, GET /healthz answers the same check
(200 or 503) for container probes.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import math
import sys
import time

from ..hermes import _config

def path():
    return _config.state_file(None, "bot_health.json")


def write(status):
    from ..healthcheck import process_identity
    status = {**status, "host": socket.gethostname(),
              "process_start": process_identity(os.getpid())}
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=1, sort_keys=True))
    os.replace(tmp, target)


def read():
    try:
        data = json.loads(path().read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (ValueError, TypeError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def evaluate(status, now=None, stale_s=None):
    """(ok, reason) for one heartbeat record."""
    now = time.time() if now is None else now
    stale_s = float(stale_s if stale_s is not None else _config.cfg()["bot"]["health_stale_s"])
    if not status:
        return False, f"no heartbeat at {path()}"
    try:
        age = now - float(status.get("heartbeat_at") or 0)
    except (ValueError, TypeError):
        return False, "invalid heartbeat timestamp"
    if not math.isfinite(age) or age < -5 or age > stale_s:
        return False, f"heartbeat is {age:.0f}s old (limit {stale_s:.0f}s)"
    try:
        pid = int(status["pid"])
        if pid <= 0:
            raise ValueError("nonpositive PID")
    except (KeyError, ValueError, TypeError, OverflowError):
        return False, "invalid bot process identity"
    if not _alive(pid):
        return False, f"bot process {status['pid']} is gone"
    if status.get("mode") != "standalone":
        return False, f"bot mode is {status.get('mode')!r}"
    if not status.get("connected"):
        return False, "not connected to Discord"
    return True, f"connected as {status.get('user') or 'unknown'}, {status.get('handlers', 0)} handlers"


def check(out=None):
    out = out or sys.stdout
    status = read()
    ok, reason = evaluate(status)
    if ok:
        from ..healthcheck import process_identity
        try:
            if (status.get("host") != socket.gethostname()
                    or process_identity(status["pid"]) != status.get("process_start")):
                ok, reason = False, "heartbeat belongs to another container or replaced process"
        except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
            ok, reason = False, "bot process identity unavailable"
    print(f"{'ok' if ok else 'FAIL'}  standalone bot: {reason}", file=out)
    return 0 if ok else 1


async def serve(port, status_func, host="0.0.0.0"):
    """A tiny HTTP server: GET /healthz -> 200 or 503 with the heartbeat JSON. Returns the server."""

    async def handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            parts = line.decode(errors="replace").split()
            if len(parts) >= 2 and parts[1].split("?", 1)[0] in ("/healthz", "/health"):
                status = status_func()
                ok, reason = evaluate(status)
                code, body = (200 if ok else 503), json.dumps({"ok": ok, "reason": reason, **(status or {})})
            else:
                code, body = 404, json.dumps({"ok": False, "reason": "try /healthz"})
            phrase = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[code]
            data = body.encode()
            writer.write(f"HTTP/1.1 {code} {phrase}\r\nContent-Type: application/json\r\n"
                         f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)
