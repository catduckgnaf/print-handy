"""Boundary integration tests against real loopback servers: no printer, no chat service, no camera.

Three edges of the listener are driven over real sockets (127.0.0.2 only and the module's own guard refuses anything else):

* Moonraker: a fake JSON-RPC websocket the listener really connects to, drops, and reconnects to.
* Chat: fake Discord, Telegram and Matrix HTTP APIs behind the real REST senders.
* Camera: a fake snapshot server behind the real watchdog snapshot path.

Anything that needs the physical printer (real firmware, real webcam, real chat accounts) is
outside these tests and still needs authorized verification.
"""
import asyncio
import io
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import websockets
from websockets.asyncio.server import serve

from print_handy.bot import matrix_rest, rest, telegram_rest
from print_handy.hermes import _config
from print_handy.hermes import moonraker_event_listener as mel

TOKEN = "tok-secret-123"
LOOPBACK = "127.0.0.2"
IDLE = {"print_stats": {"state": "standby", "filename": "", "message": "", "info": {}}}


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    """Replaces the suite guard, which also blocks any host a test points a service URL at.

    These tests point every service URL at a loopback fake, so the guard here is stricter in the
    other direction: only 127.0.0.2 is reachable, by name or address, and nothing else.
    """
    real_cc, real_connect = socket.create_connection, socket.socket.connect

    # The real senders use urllib, which honors proxy variables; keep loopback direct.
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, LOOPBACK)
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)

    def check(host):
        if host != LOOPBACK:
            raise AssertionError(f"test tried to reach {host!r}; only {LOOPBACK} is allowed")

    def create_connection(address, *a, **k):
        check(address[0])
        return real_cc(address, *a, **k)

    def connect(self, address):
        if isinstance(address, tuple):
            check(address[0])
        return real_connect(self, address)

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket.socket, "connect", connect)


def jpeg(color=(200, 120, 40)):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(out, "JPEG")
    return out.getvalue()


# --------------------------------------------------------------------------- Moonraker reconnect


class FakeMoonraker:
    """A loopback websocket that plays one scripted behavior per connection, then idles."""

    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.connections = 0
        self.subscribes = 0
        self.ready = asyncio.Event()

    async def handler(self, ws):
        index = self.connections
        self.connections += 1
        behavior = self.behaviors[index] if index < len(self.behaviors) else "idle"
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "printer.objects.subscribe":
                continue
            self.subscribes += 1
            if behavior == "refuse":
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                          "error": {"code": -32601, "message": "Klippy not ready"}}))
                await asyncio.sleep(0.2)  # let the listener read the refusal
                self.ready.set()
            elif behavior == "garbage":
                await ws.send("this is not json")
                self.ready.set()
            elif behavior == "drop":
                await ws.close()
                return
            else:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                          "result": {"eventtime": 0, "status": IDLE}}))
                if behavior == "ready_again" and self.subscribes == 1:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "notify_klippy_ready"}))
                self.ready.set()


@pytest.fixture
def listener(monkeypatch):
    """A real Listener with every printer, chat and disk side effect stubbed and all polls disabled."""
    monkeypatch.setattr(mel, "NATIVE_POLL_SECONDS", 10 ** 9)
    monkeypatch.setattr(mel, "TICK_SECONDS", 10 ** 9)
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    lst = mel.Listener()
    now = __import__("time").monotonic()
    lst.last_native_poll = lst.maintenance_last_check = now

    async def nothing(*a, **k):
        return None

    for name in ("backfill_slot_colors", "reconcile_spools", "heartbeat"):
        monkeypatch.setattr(lst, name, nothing)
    monkeypatch.setattr(lst, "_sync_printer_clock", lambda *a, **k: None)
    monkeypatch.setattr(lst, "_schedule_capture", lambda *a, **k: None)
    return lst


async def run_forever_until(lst, server, monkeypatch, done, delays):
    """Run Listener.forever() against the fake until done(server, delays) is true.

    done must test what the client has consumed (listener state, subscribes the server received),
    never just what the server sent."""
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds, *a, **k):
        if sys._getframe(1).f_code.co_name == "forever":  # the reconnect back-off only
            delays.append(seconds)
            seconds = 0.01
        await real_sleep(seconds, *a, **k)

    monkeypatch.setattr(mel.asyncio, "sleep", fast_sleep)
    task = asyncio.create_task(lst.forever())
    try:
        async with asyncio.timeout(15):
            while not done(server, delays):
                await real_sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def drive_moonraker(lst, monkeypatch, behaviors, done):
    fake = FakeMoonraker(behaviors)
    delays = []
    fake.ready = None  # created inside the loop below

    async def flow():
        fake.ready = asyncio.Event()
        async with serve(fake.handler, "127.0.0.2", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(mel, "WS_URL", f"ws://127.0.0.2:{port}/websocket")
            await run_forever_until(lst, fake, monkeypatch, done, delays)
    asyncio.run(flow())
    return fake, delays


def test_listener_reconnects_and_resubscribes_after_the_server_drops(listener, monkeypatch):
    fake, delays = drive_moonraker(listener, monkeypatch, ["drop", "drop", "idle"],
                                  lambda f, d: f.subscribes >= 3 and listener.state == "standby")
    assert fake.connections >= 3 and fake.subscribes >= 3  # every reconnect subscribed again
    assert delays[:2] == [5, 10]  # back-off doubles from five seconds
    assert listener.state == "standby"  # the third session's subscribe reply landed


def test_back_off_is_capped_and_never_gives_up(listener, monkeypatch):
    fake, delays = drive_moonraker(listener, monkeypatch, ["drop"] * 7 + ["idle"],
                                  lambda f, d: f.subscribes >= 8 and len(d) >= 7)
    assert delays[:7] == [5, 10, 20, 40, 80, 120, 120]
    assert fake.connections >= 8


def test_a_refused_subscribe_does_not_end_the_session(listener, monkeypatch):
    fake, delays = drive_moonraker(listener, monkeypatch, ["refuse"], lambda f, d: f.ready.is_set())
    assert fake.connections == 1 and delays == []  # Klippy not ready is logged, not a reconnect


def test_a_malformed_frame_drops_the_session_and_reconnects(listener, monkeypatch):
    fake, delays = drive_moonraker(listener, monkeypatch, ["garbage", "idle"],
                                  lambda f, d: f.subscribes >= 2 and listener.state == "standby")
    assert fake.connections >= 2 and delays[:1] == [5]
    assert listener.state == "standby"


def test_klippy_ready_resubscribes_on_the_same_connection(listener, monkeypatch):
    fake, delays = drive_moonraker(listener, monkeypatch, ["ready_again"],
                                  lambda f, d: f.subscribes >= 2 and listener.state == "standby")
    assert fake.connections == 1 and delays == []
    assert fake.subscribes == 2  # the first subscribe plus the one after notify_klippy_ready


def test_connection_flag_is_false_when_nothing_answers(listener, monkeypatch):
    async def flow():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.2", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()  # the port is now closed: connection refused
        monkeypatch.setattr(mel, "WS_URL", f"ws://127.0.0.2:{port}/websocket")
        with pytest.raises(OSError):
            await listener.session()
        assert listener.moonraker_connected is False

    listener.moonraker_connected = False
    asyncio.run(flow())


# --------------------------------------------------------------------------- chat boundaries


class FakeChat:
    """One loopback HTTP server standing in for the Discord, Telegram and Matrix APIs."""

    def __init__(self, status=200):
        self.status = status
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                if outer.status != 200:
                    payload = b'{"message": "boom"}'
                    self.send_response(outer.status)
                else:
                    if "/sendMessage" in self.path:
                        payload = json.dumps({"ok": True, "result": {"message_id": 77}}).encode()
                    elif "/send/m.room.message" in self.path:
                        payload = json.dumps({"event_id": "$evt1"}).encode()
                    else:
                        payload = json.dumps({"id": "msg-1"}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = _answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.2", 0), Handler)
        self.url = f"http://127.0.0.2:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def chat():
    fakes = []

    def make(status=200):
        fake = FakeChat(status)
        fakes.append(fake)
        return fake
    yield make
    for fake in fakes:
        fake.close()


def standalone(monkeypatch, platform):
    monkeypatch.setitem(_config.cfg()["bot"], "mode", "standalone")
    monkeypatch.setitem(_config.cfg()["bot"], "platform", platform)
    monkeypatch.setattr(mel.announcer, "DISCORD_CHAT_ID", "chan-1")
    monkeypatch.setitem(_config.cfg()["telegram"], "chat_id", "555")
    monkeypatch.setitem(_config.cfg()["matrix"], "room_id", "!room:test")
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: TOKEN)


def test_discord_post_reaches_the_api_with_the_bot_token(chat, monkeypatch):
    fake = chat()
    standalone(monkeypatch, "discord")
    monkeypatch.setattr(rest, "API", fake.url)
    assert mel.post("Print started: cube") is True
    method, path, headers, body = fake.requests[0]
    assert (method, path) == ("POST", "/channels/chan-1/messages")
    assert headers["Authorization"] == f"Bot {TOKEN}"
    assert json.loads(body)["content"] == "Print started: cube"
    assert json.loads(body)["allowed_mentions"] == {"parse": []}  # a print title can never ping anyone


def test_telegram_post_reaches_the_bot_api(chat, monkeypatch):
    fake = chat()
    standalone(monkeypatch, "telegram")
    monkeypatch.setattr(telegram_rest, "API", fake.url)
    assert mel.post("Print finished") is True
    _, path, _, body = fake.requests[0]
    assert path == f"/bot{TOKEN}/sendMessage"
    assert b"Print finished" in body and b"555" in body


def test_matrix_post_reaches_the_room(chat, monkeypatch):
    fake = chat()
    standalone(monkeypatch, "matrix")
    monkeypatch.setitem(_config.cfg()["matrix"], "homeserver_url", fake.url)
    assert mel.post("Print paused") is True
    method, path, headers, body = fake.requests[0]
    assert method == "PUT" and "/rooms/%21room%3Atest/send/m.room.message/" in path
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(body) == {"msgtype": "m.text", "body": "Print paused"}


@pytest.mark.parametrize("platform", ["discord", "telegram", "matrix"])
def test_a_chat_outage_returns_false_and_never_raises(chat, monkeypatch, platform):
    fake = chat(status=500)
    standalone(monkeypatch, platform)
    monkeypatch.setattr(rest, "API", fake.url)
    monkeypatch.setattr(telegram_rest, "API", fake.url)
    monkeypatch.setitem(_config.cfg()["matrix"], "homeserver_url", fake.url)
    assert mel.post("Print failed") is False  # the listener keeps running through a chat outage


@pytest.mark.parametrize("platform", ["discord", "telegram", "matrix"])
def test_an_unreachable_chat_server_returns_false(monkeypatch, platform):
    standalone(monkeypatch, platform)
    dead = FakeChat()
    url = dead.url
    dead.close()  # nothing listens on this port any more
    monkeypatch.setattr(rest, "API", url)
    monkeypatch.setattr(telegram_rest, "API", url)
    monkeypatch.setitem(_config.cfg()["matrix"], "homeserver_url", url)
    assert mel.post("Print failed") is False


def test_a_long_post_is_split_to_the_platform_limit(chat, monkeypatch):
    fake = chat()
    standalone(monkeypatch, "discord")
    monkeypatch.setattr(rest, "API", fake.url)
    assert mel.post(("line of text\n" * 400)) is True
    assert len(fake.requests) >= 3
    assert all(len(json.loads(body)["content"]) <= rest.LIMIT for _, _, _, body in fake.requests)


def test_a_missing_token_is_refused_before_any_request(chat, monkeypatch):
    fake = chat()
    standalone(monkeypatch, "discord")
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "")
    monkeypatch.setattr(rest, "API", fake.url)
    assert mel.post("Print started") is False
    assert fake.requests == []


# --------------------------------------------------------------------------- camera


class FakeCamera:
    """A loopback snapshot endpoint: /good, /dark, /html, /huge, /broken."""

    def __init__(self):
        self.hits = []
        outer = self
        self.bodies = {"/good": jpeg(), "/dark": jpeg((0, 0, 0)), "/html": b"<html>login</html>",
                       "/huge": b"\xff\xd8" + b"0" * (8 * 1024 * 1024 + 10)}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append(self.path)
                body = outer.bodies.get(self.path)
                if body is None:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.2", 0), Handler)
        self.base = f"http://127.0.0.2:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def camera(monkeypatch):
    fake = FakeCamera()
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setitem(_config.cfg()["services"], "camera_source", "auto")
    yield fake
    fake.close()


def point_camera(monkeypatch, direct, fanout=None):
    monkeypatch.setitem(_config.cfg()["services"], "camera_snapshot_url", direct)
    monkeypatch.setattr(mel.go2rtc, "fanout_frame_url", lambda *a, **k: fanout)


def test_direct_snapshot_is_returned_when_no_fanout_runs(camera, monkeypatch):
    point_camera(monkeypatch, camera.base + "/good")
    assert mel._camera_snapshot_bytes() == camera.bodies["/good"]
    assert camera.hits == ["/good"]


def test_fanout_is_the_single_pull_when_it_works(camera, monkeypatch):
    point_camera(monkeypatch, camera.base + "/broken", camera.base + "/good")
    assert mel._camera_snapshot_bytes() == camera.bodies["/good"]
    assert camera.hits == ["/good"]  # the direct camera was never opened a second time


def test_a_failing_fanout_falls_back_to_the_direct_url_once(camera, monkeypatch):
    point_camera(monkeypatch, camera.base + "/good", camera.base + "/broken")
    assert mel._camera_snapshot_bytes() == camera.bodies["/good"]
    assert camera.hits == ["/broken", "/good"]  # sequential, never parallel


def test_a_dark_fanout_frame_falls_back_to_the_direct_url(camera, monkeypatch):
    point_camera(monkeypatch, camera.base + "/good", camera.base + "/dark")
    assert mel._camera_snapshot_bytes() == camera.bodies["/good"]
    assert camera.hits == ["/dark", "/good"]


@pytest.mark.parametrize("path", ["/html", "/huge", "/broken"])
def test_bad_camera_answers_raise_instead_of_reaching_vision(camera, monkeypatch, path):
    point_camera(monkeypatch, camera.base + path)
    with pytest.raises(Exception):
        mel._camera_snapshot_bytes()


def test_an_unreachable_camera_raises(camera, monkeypatch):
    point_camera(monkeypatch, camera.base + "/good")
    camera.close()
    with pytest.raises(OSError):
        mel._camera_snapshot_bytes()


def test_camera_source_none_never_touches_the_network(camera, monkeypatch):
    point_camera(monkeypatch, camera.base + "/good", camera.base + "/good")
    monkeypatch.setitem(_config.cfg()["services"], "camera_source", "none")
    with pytest.raises(ValueError, match="camera images are off"):
        mel._camera_snapshot_bytes()
    assert camera.hits == []


def test_camera_source_moonraker_ignores_a_running_fanout(camera, monkeypatch):
    point_camera(monkeypatch, camera.base + "/good", camera.base + "/dark")
    monkeypatch.setitem(_config.cfg()["services"], "camera_source", "moonraker")
    assert mel._camera_snapshot_bytes() == camera.bodies["/good"]
    assert camera.hits == ["/good"]
