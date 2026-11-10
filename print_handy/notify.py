"""Events that already have a chat post, plus MQTT and ntfy subscribers (#622).

Each channel is off unless its own switch is on: MQTT needs [notify]
mqtt_enabled true and mqtt_host set; ntfy needs ntfy_enabled true and an
ntfy_url and a topic (validation refuses enabled-without-URL or -topic at load). Neither
channel's credential is a setting: the MQTT password is MQTT_PASSWORD
and the ntfy access token is NTFY_ACCESS_TOKEN, both from the
environment only. A delivery failure is logged and does not raise, so
chat and the print continue.
"""
from __future__ import annotations

import logging
import os
import queue
import secrets
import socket
import threading
import urllib.request

from . import credential_binding

log = logging.getLogger("print_handy.notify")

EVENTS = ("print_started", "paused", "runout", "watchdog_offer", "complete", "failed", "spool_added")

_FROM_FORMAT = {
    "started": "print_started",
    "paused": "paused",
    "complete": "complete",
    "error": "failed",
    "cancelled": "failed",
}


def event_for(kind, extra=None) -> str:
    """The bus name for a chat format_event kind, or "" when it is not on the list."""
    if kind == "paused" and extra and "runout" in str(extra).lower():
        return "runout"
    return _FROM_FORMAT.get(kind, "")


def emit(kind, summary, *, printer_id="", connector=None, http_opener=None) -> bool:
    """Publish one event to every enabled channel.

    Returns True when at least one enabled channel delivered; False when
    no channel is on or every delivery failed. ``connector`` is the MQTT
    test seam: ``connector(host, port)`` returns an object with
    ``sendall`` and ``close`` (production uses a TCP connection).
    ``http_opener`` is the ntfy test seam: ``http_opener(request,
    timeout=...)`` returns a response with a ``status`` (production uses
    urllib.request.urlopen).

    This blocks on the channels' I/O (a 3 s broker connect, a 5 s HTTP
    POST); callers on a listener tick or an event loop use
    :func:`emit_queued` instead.
    """
    try:
        return _emit(kind, summary, printer_id=printer_id, connector=connector,
                     http_opener=http_opener)
    except Exception as exc:
        log.warning("notify publish failed: %s", exc)
        return False


_queue: queue.SimpleQueue = queue.SimpleQueue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _worker_loop():
    while True:
        kind, summary, printer_id = _queue.get()
        emit(kind, summary, printer_id=printer_id)


def _ensure_worker():
    global _worker
    if _worker is None or not _worker.is_alive():
        with _worker_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_worker_loop, name="print-handy-notify",
                                           daemon=True)
                _worker.start()


def emit_queued(kind, summary, *, printer_id="") -> bool:
    """Queue one event for delivery on the shared notify worker thread.

    The broker/HTTP I/O never runs on the caller's path: a listener tick
    or an event-loop callback returns as soon as the event is queued, and
    events deliver in order on the one worker. Returns True when the
    event was queued, False for an unknown event name; the delivery
    itself is logged by :func:`emit` on the worker, exactly as a direct
    call reports it.
    """
    if kind not in EVENTS:
        return False
    try:
        _ensure_worker()
        _queue.put((kind, summary, printer_id))
    except Exception as exc:
        log.warning("notify queue failed: %s", exc)
        return False
    return True


def _settings():
    from . import settings
    data = settings.current()
    return data.get("notify") if isinstance(data, dict) else None


def _payload(kind, summary, printer_id) -> str:
    """The one-line event payload both channels carry."""
    payload = str(summary or "").splitlines()[0][:240]
    if not printer_id:
        try:
            from . import listeners
            printer_id = listeners.instance_printer_id() or ""
        except Exception:
            printer_id = ""
    if printer_id:
        payload = f"{printer_id}: {payload}"
    return payload


def _emit(kind, summary, *, printer_id, connector, http_opener) -> bool:
    if kind not in EVENTS:
        return False
    cfg = _settings() or {}
    payload = _payload(kind, summary, printer_id)
    delivered = False
    if cfg.get("mqtt_enabled") is True:
        try:
            delivered = _emit_mqtt(cfg, kind, payload, connector=connector) or delivered
        except Exception as exc:
            log.warning("mqtt publish failed: %s", exc)
    if cfg.get("ntfy_enabled") is True:
        try:
            delivered = _emit_ntfy(cfg, kind, payload, http_opener=http_opener) or delivered
        except Exception as exc:
            log.warning("ntfy publish failed: %s", exc)
    return delivered


def _emit_mqtt(cfg, kind, payload, *, connector) -> bool:
    host = str(cfg.get("mqtt_host") or "").strip()
    if not host:
        return False
    port = int(cfg.get("mqtt_port") or 1883)
    topic_root = str(cfg.get("mqtt_topic") or "print-handy").strip("/") or "print-handy"
    topic = f"{topic_root}/{kind}"
    username = str(cfg.get("mqtt_username") or "")
    password = credential_binding.key_for("notify.mqtt_host", host,
                                          os.environ.get("MQTT_PASSWORD") or "")
    packet = _publish_packet(topic, payload, username=username, password=password)
    opener = connector or (lambda h, p: socket.create_connection((h, p), timeout=3))
    sock = opener(host, port)
    try:
        sock.sendall(packet)
        recv = getattr(sock, "recv", None)
        if recv:
            code = _connack(recv(4))
            if code != 0:
                log.warning("mqtt broker refused the connection (%s)", "no CONNACK" if code is None else f"code {code}")
                return False
    finally:
        closer = getattr(sock, "close", None)
        if closer:
            closer()
    return True


def _ntfy_target(cfg) -> str:
    """The ntfy topic URL: ntfy_url, with ntfy_topic appended when set.

    ntfy_url may already name the topic (https://ntfy.sh/my-topic); when
    ntfy_topic is also set and the URL does not already end with it, the
    topic is appended (https://ntfy.sh + my-topic). No topic anywhere
    means there is nowhere to publish: the empty string.
    """
    base = str(cfg.get("ntfy_url") or "").strip().rstrip("/")
    topic = str(cfg.get("ntfy_topic") or "").strip().strip("/")
    if not base:
        return ""
    if topic and not base.endswith(f"/{topic}"):
        return f"{base}/{topic}"
    return base


def _emit_ntfy(cfg, kind, payload, *, http_opener) -> bool:
    """One ntfy publish: a plain HTTP POST of the payload to the topic URL.

    The title names the event; the body is the same one-line payload the
    MQTT channel sends. A protected topic's access token comes from
    NTFY_ACCESS_TOKEN in the environment, sent as a Bearer token — it is
    never a setting and never logged. A non-2xx answer is a failed
    delivery, logged like an MQTT refusal.
    """
    url = _ntfy_target(cfg)
    if not url:
        log.warning("ntfy publish skipped: [notify] ntfy_url names no topic "
                    "(set ntfy_topic, or include the topic in ntfy_url)")
        return False
    headers = {"Title": f"Print Handy: {kind}", "Tags": "printer"}
    token = credential_binding.key_for("notify.ntfy_url", url,
                                       os.environ.get("NTFY_ACCESS_TOKEN") or "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=payload.encode("utf-8"),
                                     headers=headers, method="POST")
    opener = http_opener or urllib.request.urlopen
    with opener(request, timeout=5) as response:
        status = getattr(response, "status", 200)
    if not 200 <= int(status) < 300:
        log.warning("ntfy server refused the publish (HTTP %s)", status)
        return False
    return True


def _connack(data):
    """The CONNACK return code, or None when the broker sent no valid CONNACK."""
    if len(data or b"") == 4 and data[0] == 0x20 and data[1] == 2:
        return data[3]
    return None


def _encode(text: str) -> bytes:
    raw = text.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def _rest(length: int) -> bytes:
    out = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        out.append(digit)
        if not length:
            return bytes(out)


def _publish_packet(topic, payload, *, username="", password="") -> bytes:
    """MQTT 3.1.1 CONNECT plus one QoS 0 PUBLISH. No subscribe, no retain."""
    flags = 0x02  # clean session
    if username:
        flags |= 0x80
        if password:
            flags |= 0x40
    variable = _encode("MQTT") + bytes([4, flags]) + (60).to_bytes(2, "big") + _encode(f"print-handy-{secrets.token_hex(4)}")
    if username:
        variable += _encode(username)
        if password:
            variable += _encode(password)
    connect = bytes([0x10]) + _rest(len(variable)) + variable
    body = _encode(topic) + payload.encode("utf-8")
    publish = bytes([0x30]) + _rest(len(body)) + body
    return connect + publish
